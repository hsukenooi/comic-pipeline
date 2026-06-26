#!/usr/bin/env python3
"""Backup-gated cleanup for the three pre-existing duplicate classes in the
collection store (BUI-125).

This is a **one-time, manual** maintenance script — not part of the everyday
`locg` CLI surface — for the data-hygiene issues a BUI-122 dry-run surfaced in
the comics-server store (`~/.comics-server/collection-store/`, with a
`~/.gixen-server` fallback until the data is physically migrated). It is
independent of the sync mechanics; it removes leftover junk the import
reconciler deliberately leaves *pending and visible* rather than merging or
duplicating.

Three classes (see packages/locg-cli/docs/processes/locg-collection-wishlist-sync.md
"Duplicate win-records cleanup"):

  1. same-book / different-identity dup wins — a pending ``agent_win`` row
     (``pushed_to_locg_at`` is null) whose ``(series, issue)`` is already owned
     by an established (``locg_export`` or already-pushed) row. The win is a
     leftover that record-win's BUI-34 dedup didn't catch (typically because the
     owned copy was imported *after* the win was recorded). Keep the owned row;
     drop the pending win.

  2. exact-identity duplicate rows — two rows sharing the full identity tuple
     ``(publisher, series, full_title, release_date)``. ``identity_to_idx`` maps
     an identity to one index, so the redundant twin never clears. Keep one
     (prefer an established/pushed row, else the earliest-added); drop the rest.

  3. owned local-only wish-list adds — a ``wish-list.json`` item with no
     ``series_name`` (a ``locg wish-list add``) whose title is actually owned.
     Harmlessly excluded from the owned-safe export (PR #59) but clutter. Drop
     it from the wish-list cache.

Dry-run by default: it prints exactly what it would drop and changes nothing.
Pass ``--apply`` to write — it makes a timestamped backup of each file it
touches first and refuses to proceed if the backup write fails, then rewrites
atomically (tempfile + os.replace).

Usage (on the server host):

    # inspect — read-only, default store dir ~/.comics-server/collection-store
    # (falls back to ~/.gixen-server/collection-store until data is migrated)
    python3 scripts/cleanup_duplicates.py

    # against an arbitrary store copy
    python3 scripts/cleanup_duplicates.py --store-dir /path/to/collection-store

    # apply, after eyeballing the dry-run
    python3 scripts/cleanup_duplicates.py --apply

Run it from a checkout where ``locg`` is importable (the server's workspace
install, or ``PYTHONPATH=packages/locg-cli/src``); it reuses locg's own
identity/normalization helpers so its matching matches the live code.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from locg.collection_cache import _normalize_series_key, make_identity
from locg.collection_io import _normalize_title
from locg.commands import _split_full_title

def _resolve_store_dir() -> Path:
    """Comics-server collection store with a safe fallback (BUI-220).

    Mirrors gixen-cli's ``server.db.resolve_server_dir`` without importing it
    (locg-cli must not depend back on gixen-cli): ``~/.comics-server`` if it
    exists, else the legacy ``~/.gixen-server`` while it still does, else the
    canonical ``~/.comics-server``. Returns the ``collection-store`` subdir.
    """
    new = Path.home() / ".comics-server"
    legacy = Path.home() / ".gixen-server"
    if new.exists():
        base = new
    elif legacy.exists():
        base = legacy
    else:
        base = new
    return base / "collection-store"


DEFAULT_STORE_DIR = _resolve_store_dir()


def _issue_key(token: str) -> str:
    """Issue-token key, matching record-win's BUI-34 dedup (commands.py)."""
    return (token.strip().lstrip("0") or token.strip()).lower()


def _is_pending_win(row: dict[str, Any]) -> bool:
    return row.get("source") == "agent_win" and not row.get("pushed_to_locg_at")


def _is_established(row: dict[str, Any]) -> bool:
    """A row whose ownership is settled on LOCG's side (so a pending win that
    duplicates it is the removable copy)."""
    return row.get("source") == "locg_export" or bool(row.get("pushed_to_locg_at"))


def _issue_identity(row: dict[str, Any]) -> Optional[tuple[str, str]]:
    """``(normalized_series, issue_key)`` from a row's full_title, or None when
    there's no ``#N`` token. Mirrors record-win's owned_index keying."""
    prefix, token = _split_full_title(row.get("full_title") or "")
    if token is None:
        return None
    return (_normalize_series_key(prefix), _issue_key(token))


def _row_label(row: dict[str, Any]) -> str:
    pub = row.get("publisher_name") or "—"
    return f"{row.get('full_title') or '(no title)'}  [pub={pub}, src={row.get('source')}, pushed={'Y' if row.get('pushed_to_locg_at') else 'N'}]"


def find_exact_identity_dups(comics: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any], int]]:
    """Class 2. Return (drop_index, dropped_row, kept_index) for each redundant
    twin. Keeps one row per identity — an established/pushed row if any, else the
    earliest-added (lowest local_added_seq)."""
    by_identity: dict[tuple[str, str, str, str], list[int]] = {}
    for i, row in enumerate(comics):
        by_identity.setdefault(make_identity(row), []).append(i)

    drops: list[tuple[int, dict[str, Any], int]] = []
    for idxs in by_identity.values():
        if len(idxs) < 2:
            continue
        # Choose the keeper: established first, then lowest local_added_seq.
        def _sort_key(i: int) -> tuple[int, Any]:
            r = comics[i]
            return (0 if _is_established(r) else 1, r.get("local_added_seq") or 0)

        keep = min(idxs, key=_sort_key)
        for i in idxs:
            if i != keep:
                drops.append((i, comics[i], keep))
    return drops


def find_owned_pending_wins(
    comics: list[dict[str, Any]], exclude: set[int]
) -> list[tuple[int, dict[str, Any], dict[str, Any]]]:
    """Class 1. Return (drop_index, dropped_row, owning_row) for each pending
    ``agent_win`` whose (series, issue) is already owned by an established row.
    ``exclude`` holds indices already slated for removal by another class."""
    # Index established owned rows by issue identity (the winning/kept copy).
    owned: dict[tuple[str, str], int] = {}
    for i, row in enumerate(comics):
        if i in exclude:
            continue
        if not row.get("in_collection") or not _is_established(row):
            continue
        key = _issue_identity(row)
        if key is not None:
            owned.setdefault(key, i)

    drops: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for i, row in enumerate(comics):
        if i in exclude or not _is_pending_win(row):
            continue
        key = _issue_identity(row)
        if key is not None and key in owned:
            drops.append((i, row, comics[owned[key]]))
    return drops


def find_owned_wish_adds(
    wish_items: list[dict[str, Any]], owned_titles: set[str]
) -> list[tuple[int, dict[str, Any]]]:
    """Class 3. Return (index, item) for each local-only wish add (no
    ``series_name``) whose normalized title is owned."""
    drops: list[tuple[int, dict[str, Any]]] = []
    for i, item in enumerate(wish_items):
        if item.get("series_name"):
            continue  # export-derived; not a local-only add
        if _normalize_title(item.get("name") or "") in owned_titles:
            drops.append((i, item))
    return drops


def _backup(path: Path, stamp: str) -> Path:
    """Copy ``path`` to a timestamped sibling. Raises on failure."""
    bak = path.with_name(f"{path.name}.dedup-bak.{stamp}")
    bak.write_text(path.read_text())
    return bak


def _write_json_atomic(path: Path, data: Any) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--store-dir",
        type=Path,
        default=Path(os.environ.get("LOCG_DATA_DIR") or DEFAULT_STORE_DIR),
        help=f"collection-store directory (default: $LOCG_DATA_DIR or {DEFAULT_STORE_DIR})",
    )
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = parser.parse_args(argv)

    store: Path = args.store_dir
    collection_path = store / "collection.json"
    wish_path = store / "wish-list.json"

    if not collection_path.exists():
        print(f"error: {collection_path} not found (is --store-dir right?)", file=sys.stderr)
        return 1

    payload = json.loads(collection_path.read_text())
    comics: list[dict[str, Any]] = payload.get("comics", [])

    wish_payload: dict[str, Any] = {}
    wish_items: list[dict[str, Any]] = []
    if wish_path.exists():
        wish_payload = json.loads(wish_path.read_text())
        wish_items = wish_payload.get("items", [])

    owned_titles = {
        _normalize_title(r.get("full_title") or "")
        for r in comics
        if r.get("in_collection")
    }

    # Class 2 first so its drops are excluded from class 1's candidate set.
    exact_dups = find_exact_identity_dups(comics)
    exclude = {i for i, _, _ in exact_dups}
    owned_wins = find_owned_pending_wins(comics, exclude)
    owned_wishes = find_owned_wish_adds(wish_items, owned_titles)

    # ---- Report -----------------------------------------------------------
    print(f"store: {store}")
    print(f"collection rows: {len(comics)}   wish-list items: {len(wish_items)}\n")

    print(f"[1] same-book / different-identity pending wins — {len(owned_wins)} to drop")
    for _, row, owner in owned_wins:
        print(f"    DROP {_row_label(row)}")
        print(f"      ↳ owned as: {_row_label(owner)}")
    print()

    print(f"[2] exact-identity duplicate rows — {len(exact_dups)} to drop")
    for _, row, keep_i in exact_dups:
        print(f"    DROP {_row_label(row)}")
        print(f"      ↳ keeping identical-identity row #{keep_i}: {_row_label(comics[keep_i])}")
    print()

    print(f"[3] owned local-only wish-list adds — {len(owned_wishes)} to drop")
    for _, item in owned_wishes:
        print(f"    DROP {item.get('name')!r}")
    print()

    total = len(owned_wins) + len(exact_dups) + len(owned_wishes)
    if total == 0:
        print("nothing to clean up.")
        return 0

    if not args.apply:
        print(f"DRY-RUN: would drop {len(owned_wins)} + {len(exact_dups)} collection rows "
              f"and {len(owned_wishes)} wish items. Re-run with --apply to write.")
        return 0

    # ---- Apply ------------------------------------------------------------
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    drop_collection = {i for i, _, _ in owned_wins} | {i for i, _, _ in exact_dups}
    drop_wish = {i for i, _ in owned_wishes}

    if drop_collection:
        bak = _backup(collection_path, stamp)
        print(f"backup: {bak}")
        payload["comics"] = [r for i, r in enumerate(comics) if i not in drop_collection]
        _write_json_atomic(collection_path, payload)
        print(f"wrote {collection_path}: {len(comics)} → {len(payload['comics'])} rows")

    if drop_wish:
        bak = _backup(wish_path, stamp)
        print(f"backup: {bak}")
        wish_payload["items"] = [it for i, it in enumerate(wish_items) if i not in drop_wish]
        _write_json_atomic(wish_path, wish_payload)
        print(f"wrote {wish_path}: {len(wish_items)} → {len(wish_payload['items'])} items")

    print("\ndone. Re-run without --apply to confirm the store is clean, then run a "
          "sync (/comic:collection-sync) and check row_count / pending_push_count.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
