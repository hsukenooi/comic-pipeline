"""Local collection cache: row store with atomic writes and crash recovery."""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import stat
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from locg.config import collection_cache_path, import_history_path

logger = logging.getLogger("locg")

SCHEMA_VERSION = 1

# LOCG's 21 export columns in canonical order (matches the Excel header row)
LOCG_COLUMNS: tuple[str, ...] = (
    "publisher_name",
    "series_name",
    "full_title",
    "release_date",
    "in_collection",
    "in_wish_list",
    "marked_read",
    "my_rating",
    "media_format",
    "price_paid",
    "date_purchased",
    "condition",
    "notes",
    "tags",
    "storage_box",
    "owner",
    "purchase_store",
    "signature",
    "slabbing",
    "grading",
    "grading_company",
)

# LOCG boolean columns stored as int 0/1 per R7
LOCG_BOOLEAN_COLUMNS: frozenset[str] = frozenset(
    {"in_collection", "in_wish_list", "marked_read", "signature", "slabbing"}
)

# User-managed columns whose drift is reported as behavioral_drift audit records
USER_MANAGED_COLUMNS: tuple[str, ...] = (
    "my_rating",
    "marked_read",
    "condition",
    "notes",
    "tags",
    "storage_box",
    "owner",
    "grading",
    "grading_company",
)

# Tracking fields appended to each row beyond the 21 LOCG columns
TRACKING_FIELDS: tuple[str, ...] = (
    "local_added_at",
    "local_added_seq",
    "pushed_to_locg_at",
    "last_seen_in_export_at",
    "source",
    "needs_manual_variant",
    "needs_manual_series_canonical",
    "metron_id",
    "gixen_item_id",
    "previous_full_title",
)

# Per-process monotonic counter for tiebreaking rows with identical timestamps
_SEQ_COUNTER: int = 0


def _next_seq() -> int:
    """Return the next per-process monotonic sequence number."""
    global _SEQ_COUNTER
    _SEQ_COUNTER += 1
    return _SEQ_COUNTER


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def make_identity(row: dict[str, Any]) -> tuple[str, str, str, str]:
    """Identity key: (publisher_name, series_name, full_title, release_date)."""
    return (
        row.get("publisher_name") or "",
        row.get("series_name") or "",
        row.get("full_title") or "",
        row.get("release_date") or "",
    )


def empty_payload() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "last_full_import": None,
        "last_import_source": None,
        "migration_in_progress": False,
        "last_writer": None,
        "series_name_index": {},
        "comics": [],
    }


# Patterns stripped from series names to produce normalized index keys
_YEAR_RANGE_RE = re.compile(r"\s*\(\d{4}\s*-\s*(\d{4}|Present)\)", re.IGNORECASE)
_VOL_RE = re.compile(r"\s*\(Vol\.\s*\d+\)", re.IGNORECASE)
# Also strip bare 4-digit year in parens: (1993)
_BARE_YEAR_RE = re.compile(r"\s*\(\d{4}\)")
# Strip a leading article so "The Incredible Hulk" and "Incredible Hulk" share
# a key. The article is load-bearing in display names but not for identity, and
# /comic:identify is inconsistent about emitting it (BUI-45).
_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)


def _normalize_series_key(series_name: str) -> str:
    """Normalize a series name for series_name_index lookup.

    Strips (Vol. N), (YYYY - YYYY), (YYYY - Present), and bare (YYYY) suffixes,
    a leading article (The/A/An), then lowercases.
    """
    s = series_name.strip()
    s = _YEAR_RANGE_RE.sub("", s)
    s = _VOL_RE.sub("", s)
    s = _BARE_YEAR_RE.sub("", s)
    s = _LEADING_ARTICLE_RE.sub("", s.strip())
    return s.strip().lower()


def base_series_name(decorated: str) -> str:
    """Strip LOCG series-name decoration to the bare catalog series string.

    LOCG's *series_name* keeps the ``(Vol. N)`` / ``(YYYY)`` / ``(YYYY - YYYY)`` /
    ``(YYYY - Present)`` decoration (e.g. ``Fantastic Four (Vol. 3) (1997 - 2012)``);
    its *full_title* does NOT (e.g. ``Fantastic Four #72``). This strips that
    parenthetical decoration so a full_title can be built from the base name.

    Verified against 1432 export rows: series_name KEEPS the decoration,
    full_title does NOT (BUI-199).

    The leading article is deliberately PRESERVED — LOCG often keeps it in the
    catalog string (e.g. ``The X-Men``, ``The Avengers``), so blindly stripping
    "The" would produce a full_title LOCG cannot match. (Contrast
    ``_normalize_series_key``, which DOES strip the article for index identity.)
    """
    s = decorated.strip()
    s = _YEAR_RANGE_RE.sub("", s)
    s = _VOL_RE.sub("", s)
    s = _BARE_YEAR_RE.sub("", s)
    return s.strip()


def base_full_title(canonical_series: str, issue_num: Optional[str]) -> str:
    """Build a LOCG-matchable full_title: base (undecorated) series + issue.

    Strips the ``(Vol. N) (YYYY - YYYY)`` decoration off ``canonical_series``
    (LOCG's full_title carries no decoration) and appends ``#<issue>`` when an
    issue number is present.
    """
    base = base_series_name(canonical_series)
    return f"{base} #{issue_num}" if issue_num else base


# Year-range extraction for volume resolution: captures the begin year and the
# end token (a 4-digit year or "Present") from a decorated series name.
_YEAR_RANGE_CAPTURE_RE = re.compile(
    r"\((\d{4})\s*-\s*(\d{4}|Present)\)", re.IGNORECASE
)


def series_year_range(decorated: str) -> Optional[tuple[int, int]]:
    """Extract (begin_year, end_year) from a decorated series name, or None.

    ``(YYYY - Present)`` and a bare ``(YYYY)`` both yield an open-ended range
    whose end is a far-future sentinel (9999) so any in-era year is contained.
    """
    m = _YEAR_RANGE_CAPTURE_RE.search(decorated)
    if m:
        begin = int(m.group(1))
        end_tok = m.group(2)
        end = 9999 if end_tok.lower() == "present" else int(end_tok)
        return (begin, end)
    bare = _BARE_YEAR_RE.search(decorated)
    if bare:
        yr = int(bare.group(0).strip().strip("()"))
        return (yr, 9999)
    return None


def _issue_num_int(issue_num: str) -> Optional[int]:
    """Leading integer of an issue token (``"107"`` -> 107, ``"107a"`` -> 107)."""
    m = re.match(r"\s*(\d+)", str(issue_num))
    return int(m.group(1)) if m else None


def _coerce_year(year: Any) -> Optional[int]:
    """Coerce an identify/cover year (int, "1979", "1979-05-10") to an int year."""
    if year is None:
        return None
    m = re.match(r"(\d{4})", str(year).strip())
    return int(m.group(1)) if m else None


# The LOCG X-Men series split (BUI-199): LOCG files X-Men #1–141 under
# "The X-Men (Vol. 1) (1963 - 1981)" and #142+ under
# "Uncanny X-Men (Vol. 1) (1980 - 2011)". A single index entry collapses both
# onto one canonical name; this models the issue-number boundary so the right
# series/full_title is chosen by issue number. /comic:identify emits either
# masthead ("X-Men" or "Uncanny X-Men"), so both normalized keys trigger it:
# "x-men" (leading "The" stripped) and "uncanny x-men".
_XMEN_SPLIT_KEYS = frozenset({"x-men", "uncanny x-men"})
_XMEN_SPLIT_BOUNDARY = 141
_XMEN_EARLY_SERIES = "The X-Men (Vol. 1) (1963 - 1981)"
_XMEN_LATE_SERIES = "Uncanny X-Men (Vol. 1) (1980 - 2011)"


def resolve_series_for_win(
    norm_key: str,
    issue_num: Optional[str],
    year: Any,
    series_name_index: dict[str, str],
    volume_candidates: Optional[dict[str, list[str]]] = None,
) -> Optional[str]:
    """Resolve the LOCG canonical *series_name* for a win.

    Returns the decorated canonical series name, or None when ``norm_key`` is
    unknown (the caller then falls back to Metron / manual resolution).

    Beyond a plain ``series_name_index`` lookup this corrects two BUI-199
    volume-mislabeling failures:

    * **Volume by era** — the one-to-one index collapses every volume of a
      series onto whichever one was indexed (e.g. 1979 ``Iron Man #124`` tagged
      ``(Vol. 8) (2026 - Present)``). When ``year`` is known and
      ``volume_candidates`` holds more than one volume for the key, pick the
      volume whose ``(begin - end)`` range contains the year.
    * **The X-Men split** — choose ``The X-Men`` vs ``Uncanny X-Men`` by the
      issue-number boundary (#1–141 vs #142+), independent of the index entry.

    ``volume_candidates`` (norm_key -> [decorated names]) is the multi-volume
    map from :func:`build_volume_candidates`; when omitted the resolver behaves
    like a plain index lookup (plus the X-Men split).
    """
    # The X-Men split is decided purely by issue number — the index can only
    # hold one of the two split series under the shared "x-men" key.
    if norm_key in _XMEN_SPLIT_KEYS and issue_num:
        n = _issue_num_int(issue_num)
        if n is not None:
            return _XMEN_EARLY_SERIES if n <= _XMEN_SPLIT_BOUNDARY else _XMEN_LATE_SERIES

    candidates: list[str] = []
    if volume_candidates and norm_key in volume_candidates:
        candidates = list(volume_candidates[norm_key])
    elif norm_key in series_name_index:
        candidates = [series_name_index[norm_key]]

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # Multiple volumes share this key — disambiguate by year/era when we can.
    yr = _coerce_year(year)
    if yr is not None:
        for cand in candidates:
            rng = series_year_range(cand)
            if rng and rng[0] <= yr <= rng[1]:
                return cand
    # No year, or no range contains it — fall back to the single index entry.
    return series_name_index.get(norm_key) or candidates[0]


def rebuild_series_name_index(payload: dict[str, Any]) -> dict[str, str]:
    """Rebuild series_name_index from source='locg_export' rows only (R61)."""
    index: dict[str, str] = {}
    for row in payload.get("comics", []):
        if row.get("source") == "locg_export":
            sn = row.get("series_name") or ""
            if sn:
                key = _normalize_series_key(sn)
                index[key] = sn
    return index


def build_volume_candidates(payload: dict[str, Any]) -> dict[str, list[str]]:
    """Map each normalized series key -> every distinct canonical volume name.

    Unlike :func:`rebuild_series_name_index` (one-to-one, last writer wins),
    this preserves ALL volumes filed under a key so a win can be matched to the
    volume whose year-range contains the issue's era (BUI-199). Built from
    source='locg_export' rows only (R61).
    """
    multi: dict[str, list[str]] = {}
    for row in payload.get("comics", []):
        if row.get("source") != "locg_export":
            continue
        sn = row.get("series_name") or ""
        if not sn:
            continue
        key = _normalize_series_key(sn)
        bucket = multi.setdefault(key, [])
        if sn not in bucket:
            bucket.append(sn)
    return multi


def _write_atomic(dest: Path, content: str) -> None:
    """Write a string to dest atomically via tempfile + os.replace."""
    fd, tmp = tempfile.mkstemp(prefix=".bak-", suffix=".tmp", dir=dest.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, dest)


def _write_payload_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON payload to path atomically with fsync on both fd and parent dir."""
    fd, tmp = tempfile.mkstemp(
        prefix=".collection-", suffix=".json.tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        # fsync the parent directory so the rename is durable
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


class CollectionCache:
    """Row store for the local LOCG collection cache.

    All mutations go through :meth:`apply`, which holds an exclusive flock
    for the full read-mutate-write cycle to prevent lost updates from
    concurrent processes.
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        lock_path: Optional[Path] = None,
        audit_path: Optional[Path] = None,
    ) -> None:
        self.path = path or collection_cache_path()
        self.lock_path = lock_path or (self.path.parent / "collection.lock")
        self.audit_path = audit_path or import_history_path()

    # ----- Backup helpers --------------------------------------------------

    def _bak_path(self, n: int) -> Path:
        return self.path.parent / f"{self.path.name}.bak.{n}"

    def _rotate_backups(self) -> None:
        """Rotate .bak chain before a write: .bak.1→.bak.2, .bak.0→.bak.1, live→.bak.0.

        Each step is atomic (os.replace).  A crash mid-rotation leaves the
        chain partially rotated, which is safe: older backups survive.
        """
        bak2 = self._bak_path(2)
        bak1 = self._bak_path(1)
        bak0 = self._bak_path(0)

        if bak1.exists():
            os.replace(bak1, bak2)
        if bak0.exists():
            os.replace(bak0, bak1)
        if self.path.exists():
            _write_atomic(bak0, self.path.read_text())

    # ----- Public API ------------------------------------------------------

    def load(self) -> dict[str, Any]:
        """Load and validate the cache from disk.

        Does not acquire a lock; for read-only access.  Raises RuntimeError
        on schema incompatibility or unrecoverable crash state.
        """
        if not self.path.exists():
            return empty_payload()

        try:
            with open(self.path) as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Cache file {self.path} is corrupt ({exc}). "
                f"Restore from {self._bak_path(0)} or re-import from your most recent LOCG export."
            ) from exc

        sv = payload.get("schema_version", 0)
        if sv > SCHEMA_VERSION:
            raise RuntimeError(
                f"Cache schema_version {sv} is newer than this locg-cli supports "
                f"(max: {SCHEMA_VERSION}). Upgrade locg-cli OR delete "
                f"'{self.path}' and re-import from your most recent LOCG export "
                f"(path in last_import_source)."
            )

        if payload.get("migration_in_progress"):
            self._handle_migration_flag(payload)

        return payload

    def _handle_migration_flag(self, payload: dict[str, Any]) -> None:
        """Resolve a migration_in_progress=True flag found on disk load.

        If .bak.0's last_full_import matches the live file, the process was
        killed before the merge began — auto-clear.  Otherwise abort.
        """
        bak0 = self._bak_path(0)
        bak0_last: Any = None
        if bak0.exists():
            try:
                bak0_payload = json.loads(bak0.read_text())
                bak0_last = bak0_payload.get("last_full_import")
            except (OSError, json.JSONDecodeError):
                pass

        live_last = payload.get("last_full_import")
        if bak0_last == live_last:
            # Killed before the merge began — no data was changed
            payload["migration_in_progress"] = False
            logger.warning(
                "migration_in_progress flag found but .bak.0 matches live "
                "(killed before merge began); flag auto-cleared."
            )
            self.append_audit({
                "type": "migration_in_progress_auto_cleared",
                "ts": _utcnow_iso(),
                "command": "load",
                "details": {"path": str(self.path)},
            })
        else:
            raise RuntimeError(
                f"Previous import operation crashed mid-merge. "
                f"Restore from {self._bak_path(0)} or re-import from "
                f"'{payload.get('last_import_source', 'your most recent LOCG export')}'."
            )

    def apply(
        self,
        mutate_fn: Callable[[dict[str, Any]], None],
        command: str = "unknown",
        timeout: float = 30.0,
    ) -> None:
        """Acquire exclusive flock, load, call mutate_fn, rotate .bak, write atomically.

        The full read-mutate-write cycle runs under the lock so concurrent
        callers cannot lose updates.  migration_in_progress is always False
        in the written payload (flag is set and cleared in memory only —
        there is no on-disk state of "merged but flagged").
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.lock_path.exists():
            self.lock_path.touch()

        lock_file = open(self.lock_path)  # noqa: WPS515
        try:
            # Acquire exclusive lock with timeout via non-blocking poll
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("another locg-cli operation in progress") from None
                    time.sleep(0.05)

            try:
                # Rotate backups BEFORE loading so .bak.0 captures pre-merge state
                self._rotate_backups()

                # Load current state under lock
                if not self.path.exists():
                    payload = empty_payload()
                else:
                    try:
                        with open(self.path) as f:
                            payload = json.load(f)
                    except (OSError, json.JSONDecodeError) as exc:
                        raise RuntimeError(
                            f"Cache file {self.path} is corrupt ({exc}). "
                            f"Restore from {self._bak_path(0)} or re-import from your most recent LOCG export."
                        ) from exc

                # Mutate in memory
                mutate_fn(payload)

                # Set final metadata — migration_in_progress is always False on disk
                payload["migration_in_progress"] = False
                payload["last_writer"] = {
                    "pid": os.getpid(),
                    "ts": _utcnow_iso(),
                    "command": command,
                }

                _write_payload_atomic(self.path, payload)

            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
        finally:
            lock_file.close()

    def write_wins(self, rows: list[dict[str, Any]], command: str = "record-win") -> None:
        """Insert or update a batch of agent_win rows under exclusive lock.

        Duplicate detection is by gixen_item_id: an existing row with the
        same ID is overwritten; rows without a gixen_item_id are always
        appended.  Callers are responsible for chunking large batches.
        """
        def mutate(payload: dict[str, Any]) -> None:
            idx_by_gixen: dict[str, int] = {
                row["gixen_item_id"]: i
                for i, row in enumerate(payload["comics"])
                if row.get("gixen_item_id")
            }
            for row in rows:
                gixen_id = row.get("gixen_item_id")
                if gixen_id and gixen_id in idx_by_gixen:
                    payload["comics"][idx_by_gixen[gixen_id]] = row
                else:
                    payload["comics"].append(row)

        self.apply(mutate, command=command)

    def append_audit(self, record: dict[str, Any]) -> None:
        """Append a JSON audit record to import-history.jsonl.

        Each record must have {type, ts, command, details}.  A single
        os.write of <4KB is POSIX-atomic on most filesystems.
        """
        required = {"type", "ts", "command", "details"}
        missing = required - set(record.keys())
        if missing:
            raise ValueError(f"Audit record missing keys: {missing}")

        self.audit_path.parent.mkdir(parents=True, exist_ok=True)

        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
        fd = os.open(str(self.audit_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
