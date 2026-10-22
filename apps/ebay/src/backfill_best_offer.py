#!/usr/bin/env python3
"""backfill-best-offer: fill `comps.best_offer` from the BUI-614 raw capture
(BUI-1280).

The x0.89 best-offer scale on slab comps (BUI-1234) reads `comps.best_offer`,
but only a minority of ledger rows carry the flag: most were ingested before
the flag existed. The raw capture (`~/.local/share/ebay-sold-comps-capture`)
still holds `bestOfferAccepted` for each sold item, keyed by `itemId`, which
is the ledger's `product_id`. This script joins the two and posts one
FILL-ONLY compare-and-set item per ledger row whose `best_offer` is NULL and
whose product id is in the capture: `expected: null`, `new: 0|1`. A stored
value is never rewritten (KTD4) - the server refuses anything else.

Usage:
    uv run --with requests python src/backfill_best_offer.py            # dry run
    uv run --with requests python src/backfill_best_offer.py --apply    # write
    ... --pool slab        # restrict to one pool (default: every pool)

Dry run (default) posts the batches to `POST /api/comics/comps/restamp` with
`dry_run: true`: the server computes the exact partition and writes nothing.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

import requests

from restamp_comps import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_PAGE_SIZE,
    _WRITE_TIMEOUT,
    _chunks,
    _health_gate,
    _iter_all_comps,
)
from seller_scan import _server_base

DEFAULT_CAPTURE = Path.home() / ".local" / "share" / "ebay-sold-comps-capture"
DEFAULT_SHOW = 20


def load_capture_flags(capture_dir: Path | str = DEFAULT_CAPTURE) -> dict[str, bool]:
    """item id -> bestOfferAccepted, from every raw_responses* file."""
    flags: dict[str, bool] = {}
    for f in sorted(glob.glob(str(Path(capture_dir) / "raw_responses*"))):
        op = gzip.open if f.endswith(".gz") else open
        with op(f, "rt") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                resp = rec.get("response") if isinstance(rec, dict) else None
                for it in (resp or {}).get("items") or []:
                    if it.get("itemId") and isinstance(it.get("bestOfferAccepted"), bool):
                        flags[str(it["itemId"])] = it["bestOfferAccepted"]
    return flags


def plan(
    rows: Iterable[dict], flags: dict[str, bool], *, pool: str | None = None
) -> tuple[list[dict], dict[str, Counter], int]:
    """Return `(items, counts_by_pool, not_in_ledger)`.

    `items` are fill-only restamp items (each tagged with a `_pool` and
    `_product_id` for reporting - stripped before posting). Counts per pool:
    would_set_true / would_set_false / already_set / no_capture_match.
    `not_in_ledger` is capture ids matching no scanned ledger row.
    """
    items: list[dict] = []
    counts: dict[str, Counter] = {}
    seen: set[str] = set()
    for row in rows:
        p = row.get("pool")
        if pool and p != pool:
            continue
        c = counts.setdefault(p, Counter())
        pid = str(row.get("product_id"))
        if pid in flags:
            seen.add(pid)
        if row.get("best_offer") is not None:
            c["already_set"] += 1
        elif pid in flags:
            c["would_set_true" if flags[pid] else "would_set_false"] += 1
            items.append({
                "id": row["id"], "field": "best_offer",
                "expected": None, "new": 1 if flags[pid] else 0,
                "_pool": p, "_product_id": pid,
            })
        else:
            c["no_capture_match"] += 1
    return items, counts, len(set(flags) - seen)


def _wire(item: dict) -> dict:
    return {k: v for k, v in item.items() if not k.startswith("_")}


def _print_counts(counts: dict[str, Counter], not_in_ledger: int) -> None:
    for p in sorted(counts, key=str):
        c = counts[p]
        print(
            f"pool={p}: would_set_true={c['would_set_true']} "
            f"would_set_false={c['would_set_false']} "
            f"already_set={c['already_set']} "
            f"no_capture_match={c['no_capture_match']}"
        )
    print(f"not_in_ledger (capture ids with no ledger row in scope): {not_in_ledger}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Backfill comps.best_offer from the BUI-614 capture "
                    "(fill-only NULL -> 0/1, BUI-1280)."
    )
    parser.add_argument("--apply", action="store_true",
                        help="Write. Default is a dry run (server dry_run=true).")
    parser.add_argument("--pool", choices=("raw", "slab"), default=None)
    parser.add_argument("--capture-dir", default=str(DEFAULT_CAPTURE))
    parser.add_argument("--show", type=int, default=DEFAULT_SHOW,
                        help="Per-row lines to print (default %(default)s).")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args(argv)

    base = _server_base()
    if not base:
        print("Error: COMICS_SERVER_URL is not set - cannot reach the comps "
              "ledger API.", file=sys.stderr)
        sys.exit(1)
    _health_gate(base)

    flags = load_capture_flags(args.capture_dir)
    if not flags:
        print(f"Error: no bestOfferAccepted flags found under {args.capture_dir}.",
              file=sys.stderr)
        sys.exit(1)
    items, counts, not_in_ledger = plan(
        _iter_all_comps(base, page_size=args.page_size), flags, pool=args.pool,
    )
    print(f"capture flags: {len(flags)} item ids")
    _print_counts(counts, not_in_ledger)
    for it in items[:args.show]:
        print(f"  id={it['id']} pool={it['_pool']} product_id={it['_product_id']}"
              f" best_offer NULL -> {it['new']}")
    if len(items) > args.show:
        print(f"  (+{len(items) - args.show} more)")
    if not items:
        print("Nothing to backfill.")
        return

    dry_run = not args.apply
    matched = changed = skipped_stale = not_found = 0
    for chunk in _chunks([_wire(i) for i in items], args.batch_size):
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
    print("{}: matched={} changed={} skipped_stale={} not_found={}".format(
        "DRY RUN (nothing written)" if dry_run else "APPLIED",
        matched, changed, skipped_stale, not_found,
    ))


if __name__ == "__main__":
    main()
