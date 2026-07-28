#!/usr/bin/env python3
"""BUI-555 one-time reconciliation: repair bids.max_bid rows that diverged from
Gixen and can no longer heal themselves.

Why this exists as well as the per-sync mirror
----------------------------------------------
`_sync_gixen` now mirrors Gixen's listed max_bid onto the live row every cycle
(`mirror_gixen_max_bid`), but that mirror is deliberately PENDING-only: a
resolved row keeps the cap it was actually bid at, and the sync must never
rewrite history off a later scrape. So the mirror repairs the still-live
divergences by itself on the next cycle — and this script exists for the rest:
rows that already resolved WON/LOST/ENDED carrying the stale cap, which is what
made the dashboard show a winning_bid above max_bid on a WON, and what corrupts
the BUI-532 calibration report (it measures max_bid vs winning_bid exceedance).

Gixen still lists recently-ended snipes with their status, so a single scrape
covers both the live and the just-resolved rows.

Safety
------
* DRY RUN BY DEFAULT. Nothing is written without an explicit ``--apply``.
* Only rows whose item_id Gixen currently lists are considered — this cannot
  invent a value for anything Gixen no longer knows about.
* Tombstoned rows (PURGED/REMOVED) are never touched.
* When an item_id has MORE THAN ONE non-tombstoned row (a re-listed item: the
  old auction's row plus the new one), the script REFUSES to guess which the
  listing refers to and reports it as AMBIGUOUS. Their caps are legitimately
  different and picking wrong would rewrite a real historical bid. This is the
  BUI-500 lesson applied: key on a genuinely unique target or assert exactly
  one match.

Usage (from the repo root, on the Mac Mini — the DB lives there):

    uv run --project . python packages/gixen-cli/scripts/reconcile_max_bid.py
    uv run --project . python packages/gixen-cli/scripts/reconcile_max_bid.py --apply

Run the dry run first, read the table, and only then re-run with ``--apply``.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from dotenv import load_dotenv  # noqa: E402

from gixen_client import GixenClient, parse_listed_max_bid  # noqa: E402
from server.db import (  # noqa: E402
    _MAX_BID_EPSILON, DB_PATH, TOMBSTONE_STATUSES_SQL, write_transaction,
)


def _rows_for_item(conn: sqlite3.Connection, item_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, item_id, status, max_bid, winning_bid, max_bid_changed_at "
        f"FROM bids WHERE item_id=? AND status NOT IN ({TOMBSTONE_STATUSES_SQL}) "
        "ORDER BY id",
        (item_id,),
    ).fetchall()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="BUI-555: reconcile bids.max_bid against Gixen's live list.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write the repairs. Omit for a dry run (the default).",
    )
    parser.add_argument(
        "--db-path", default=os.getenv("DB_PATH") or str(DB_PATH),
        help="Path to the comics server SQLite DB (default: %(default)s).",
    )
    args = parser.parse_args(argv)

    load_dotenv()
    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"ERROR: no DB at {db_path}", file=sys.stderr)
        return 2

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"BUI-555 max_bid reconciliation — {mode}")
    print(f"  db:    {db_path}")

    client = GixenClient()
    snipes = client.list_snipes()
    print(f"  gixen: {len(snipes)} snipe(s) listed\n")
    if not snipes:
        # An empty list is far more likely a scrape/session glitch than a
        # genuinely empty account, and acting on it would mean "reconcile
        # nothing" anyway — say so rather than printing a clean zero.
        print("Gixen returned an EMPTY list — refusing to draw conclusions. "
              "Re-run once the scrape returns snipes.")
        return 1

    divergent: list[tuple[sqlite3.Row, float]] = []
    ambiguous: list[tuple[str, float, list[sqlite3.Row]]] = []
    unreadable: list[str] = []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for snipe in snipes:
            item_id = snipe["item_id"]
            gixen_max = parse_listed_max_bid(snipe.get("max_bid"))
            if gixen_max is None:
                unreadable.append(item_id)
                continue
            rows = _rows_for_item(conn, item_id)
            candidates = [
                r for r in rows if abs(float(r["max_bid"]) - gixen_max) >= _MAX_BID_EPSILON
            ]
            if not candidates:
                continue
            if len(rows) > 1:
                ambiguous.append((item_id, gixen_max, rows))
                continue
            divergent.append((candidates[0], gixen_max))
    finally:
        conn.close()

    if unreadable:
        print(f"{len(unreadable)} listed snipe(s) had no readable max_bid "
              f"(skipped): {', '.join(unreadable)}\n")

    if ambiguous:
        print(f"AMBIGUOUS — {len(ambiguous)} item(s) have several non-tombstoned "
              f"rows; NOT touched, fix by hand:")
        for item_id, gixen_max, rows in ambiguous:
            detail = "; ".join(
                f"id={r['id']} {r['status']} max_bid={r['max_bid']}" for r in rows
            )
            print(f"  {item_id}: gixen={gixen_max}  rows: {detail}")
        print()

    if not divergent:
        print("No unambiguous divergences found. Nothing to do.")
        return 0

    print(f"{len(divergent)} divergent row(s):")
    print(f"  {'item_id':<16}{'row':>6}  {'status':<9}{'db':>10}{'gixen':>10}"
          f"{'winning':>10}")
    for row, gixen_max in divergent:
        winning = "-" if row["winning_bid"] is None else f"{row['winning_bid']:.2f}"
        print(f"  {row['item_id']:<16}{row['id']:>6}  {row['status']:<9}"
              f"{row['max_bid']:>10.2f}{gixen_max:>10.2f}{winning:>10}")
    print()

    if not args.apply:
        print("DRY RUN — nothing written. Re-run with --apply to repair these rows.")
        return 0

    now = datetime.now(timezone.utc).isoformat()
    with write_transaction(db_path) as wconn:
        for row, gixen_max in divergent:
            wconn.execute(
                "UPDATE bids SET max_bid=?, max_bid_changed_at=? WHERE id=?",
                (gixen_max, now, row["id"]),
            )
    print(f"APPLIED — repaired {len(divergent)} row(s) at {now}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
