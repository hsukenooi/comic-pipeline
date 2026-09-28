#!/usr/bin/env python3
"""restamp-comps: recompute the comps ledger's grade/label/page_quality
against today's parsers and restamp the rows that drifted (BUI-998/BUI-1008).

Two rules, one client, one endpoint:

  * BUI-1008 — BUI-1003 (PR #600) fixed `sold_comps.parse_grade` for
    split-grade phrasings (F/VF, VF-NM, ...). Every `pool='raw'` row already
    in the ledger keeps its OLD grade until the same sale is re-fetched, so
    this walks every raw row and diffs the stored `grade` against a fresh
    `parse_grade(title)`.
  * BUI-998 — the `include_graded`-only fetch never stamped `label`/
    `page_quality` on a slab comp at all (see `apps/fmv/src/fmv_runner.py`'s
    `_slab_comps_only`, fixed at the source in `sold_comps._run`). This
    walks every `pool='slab'` row and diffs stored `label`/`page_quality`
    against a fresh `parse_slab_fields(title)`. `certifier` is deliberately
    NOT touched here — BUI-997's own migration already restamped it.

Both rules live in `apps/ebay` (this package) and the comics server cannot
import them (it is not a workspace member of this package, and apps/fmv only
ever shells out to `ebay-sold-comps` — see CLAUDE.md's "FMV pipeline shells
out across package boundaries"). So the RECOMPUTE happens here, client-side,
against a full read of the ledger (`GET /api/comics/comps/all`, BUI-1008 —
the smallest enumeration path added alongside the generic restamp endpoint
this script posts to), and only the DIFF — one compare-and-set item per
changed field — is posted to `POST /api/comics/comps/restamp`.

Usage:
    uv run --with requests python src/restamp_comps.py            # dry run
    uv run --with requests python src/restamp_comps.py --apply    # write

Resolves the comics server the same way `seller_scan._server_base` /
`comics-api` do: `COMICS_SERVER_URL` (canonical) or `GIXEN_SERVER_URL`
(deprecated alias), and fails loudly — never a silent empty-host request
(the BUI-352 trap) — if neither is set or the server does not answer
`/health`.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Iterator
from typing import NamedTuple

import requests

from seller_scan import _server_base  # BUI-220 (reused, not duplicated — see
                                       # wishlist_sellers.py's own import of
                                       # the same helper within this package)
from sold_comps import parse_grade, parse_slab_fields

DEFAULT_PAGE_SIZE = 500
DEFAULT_BATCH_SIZE = 200
_HEALTH_TIMEOUT = 10
_READ_TIMEOUT = 30
_WRITE_TIMEOUT = 60
_REPORT_ID_SAMPLE = 20


def _health_gate(base: str) -> None:
    """Fail loudly, before anything else, if the server is unreachable —
    same posture `comics_health_gate`/`comics-api` take, never a request
    against a server we haven't confirmed is up."""
    try:
        resp = requests.get(f"{base}/health", timeout=_HEALTH_TIMEOUT)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(
            f"Error: the comics server at {base} is not responding ({e}). "
            "Confirm it is running before continuing.",
            file=sys.stderr,
        )
        sys.exit(1)


def _iter_all_comps(base: str, *, page_size: int = DEFAULT_PAGE_SIZE) -> Iterator[dict]:
    """Page through the WHOLE comps ledger via `GET /api/comics/comps/all`
    (BUI-1008) — cursor-based on `id`, stable under concurrent inserts."""
    after_id = 0
    while True:
        resp = requests.get(
            f"{base}/api/comics/comps/all",
            params={"after_id": after_id, "limit": page_size},
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return
        yield from rows
        after_id = rows[-1]["id"]


def _grade_diff(row: dict) -> tuple[dict | None, dict | None]:
    """For one `pool='raw'` row, return `(restamp_item, unresolved_row)`.

    `restamp_item` is the compare-and-set item to post, or None if the
    stored grade already agrees with `parse_grade(title)`. `unresolved_row`
    is the row itself, but ONLY when `parse_grade` now returns None for a
    title that carries a stored, non-null grade — the one case this script
    must NEVER post as a diff (posting `new: None` would overwrite a real
    stored grade with nothing), so it is reported separately instead.
    """
    stored = row.get("grade")
    rule = parse_grade(row.get("title") or "")
    if rule is None:
        return None, (row if stored is not None else None)
    if rule == stored:
        return None, None
    return {"id": row["id"], "field": "grade", "expected": stored, "new": rule}, None


def _slab_diffs(row: dict) -> list[dict]:
    """For one `pool='slab'` row, return the compare-and-set items for any
    of `label`/`page_quality` that disagree with `parse_slab_fields(title)`.

    `certifier` is intentionally excluded — BUI-997's migration already
    restamped every certifier='none' slab row, and `COMPS_RESTAMP_FIELDS`
    on the server does not accept it as a field for this reason."""
    fresh = parse_slab_fields(row.get("title") or "")
    diffs = []
    for field in ("label", "page_quality"):
        stored = row.get(field)
        rule = fresh[field]
        if rule != stored:
            diffs.append({
                "id": row["id"], "field": field, "expected": stored, "new": rule,
            })
    return diffs


def _chunks(items: list[dict], size: int) -> Iterator[list[dict]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


class ScanResult(NamedTuple):
    """One full `_scan` pass. Named (not a bare tuple) so the BEFORE and
    AFTER call sites in `main` can't silently transpose fields on a reorder
    — both scans share the exact same reporting code via `_print_diff_counts`/
    `_warn_ungradeable` below instead of re-deriving it."""
    grade_items: list[dict]
    label_items: list[dict]
    pq_items: list[dict]
    ungradeable: list[dict]
    raw_scanned: int
    slab_scanned: int

    @property
    def all_items(self) -> list[dict]:
        return self.grade_items + self.label_items + self.pq_items


def _scan(base: str, *, page_size: int) -> ScanResult:
    """One full pass over the ledger."""
    grade_items: list[dict] = []
    label_items: list[dict] = []
    pq_items: list[dict] = []
    ungradeable: list[dict] = []
    raw_scanned = slab_scanned = 0
    for row in _iter_all_comps(base, page_size=page_size):
        pool = row.get("pool")
        if pool == "raw":
            raw_scanned += 1
            item, unresolved = _grade_diff(row)
            if item is not None:
                grade_items.append(item)
            if unresolved is not None:
                ungradeable.append(unresolved)
        elif pool == "slab":
            slab_scanned += 1
            for d in _slab_diffs(row):
                (label_items if d["field"] == "label" else pq_items).append(d)
    return ScanResult(grade_items, label_items, pq_items, ungradeable,
                       raw_scanned, slab_scanned)


def _report_ids(rows: Iterable[dict]) -> str:
    ids = [r["id"] for r in rows]
    shown = ids[:_REPORT_ID_SAMPLE]
    suffix = f" (+{len(ids) - _REPORT_ID_SAMPLE} more)" if len(ids) > _REPORT_ID_SAMPLE else ""
    return f"{shown}{suffix}"


def _print_diff_counts(label: str, scan: ScanResult, *, show_total: bool) -> None:
    total = f" total={len(scan.all_items)}" if show_total else ""
    print(
        f"{label}: grade diffs={len(scan.grade_items)} "
        f"label diffs={len(scan.label_items)} "
        f"page_quality diffs={len(scan.pq_items)}{total}"
    )


def _warn_ungradeable(scan: ScanResult, *, verb: str) -> None:
    if not scan.ungradeable:
        return
    print(
        f"{verb}: {len(scan.ungradeable)} raw row(s) carry a stored grade "
        "but parse_grade(title) now returns None — never overwrite a stored "
        f"grade with null; ids: {_report_ids(scan.ungradeable)}",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Restamp comps ledger grade/label/page_quality against "
                    "today's parsers (BUI-998/BUI-1008)."
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Write the restamp. Default is a dry run: compute and report "
             "the diff, post it to the server in dry_run mode, write nothing.",
    )
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args(argv)

    base = _server_base()
    if not base:
        print(
            "Error: COMICS_SERVER_URL is not set — cannot reach the comps "
            "ledger API.\nSet it in ~/.zshrc (MacBook → "
            "http://mac-mini.tail9b7fa5.ts.net:8080; Mac Mini → "
            "http://localhost:8080).",
            file=sys.stderr,
        )
        sys.exit(1)
    _health_gate(base)

    before = _scan(base, page_size=args.page_size)
    print(f"scanned {before.raw_scanned} raw rows, {before.slab_scanned} slab rows")
    _print_diff_counts("BEFORE", before, show_total=True)
    _warn_ungradeable(before, verb="WARNING")

    if not before.all_items:
        print("Nothing to restamp.")
        return

    dry_run = not args.apply
    matched = changed = skipped_stale = not_found = 0
    for chunk in _chunks(before.all_items, args.batch_size):
        resp = requests.post(
            f"{base}/api/comics/comps/restamp",
            json={"dry_run": dry_run, "items": chunk},
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
        matched += body["matched"]
        changed += body["changed"]
        skipped_stale += body["skipped_stale"]
        not_found += body["not_found"]

    print(
        "{}: matched={} changed={} skipped_stale={} not_found={}".format(
            "DRY RUN (nothing written)" if dry_run else "APPLIED",
            matched, changed, skipped_stale, not_found,
        )
    )

    if args.apply:
        after = _scan(base, page_size=args.page_size)
        _print_diff_counts("AFTER (re-read from the server)", after, show_total=False)
        _warn_ungradeable(after, verb="NOTE")


if __name__ == "__main__":
    main()
