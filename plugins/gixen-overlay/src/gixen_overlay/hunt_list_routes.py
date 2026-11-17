"""BUI-1420: the hunt list dashboard tab.

`GET /api/comics/hunt-list` joins the hunt list (comics with both
`slab_min_grade` and `slab_ceiling` set, not hand-excluded) to what
`ebay-slab-alerts` logged in `slab_alerts` (BUI-1419). `GET /comics/hunt-list`
serves the page. Read-only and offline: no comic-fmv, no network call.

Market value: the non-null `slab_alerts.market` (with its `confidence` and the
copy's `grade`) of the comic's copy whose grade is nearest at or above
`slab_min_grade` (else nearest below, else ungraded), newest sighting first on
ties. It is the scanner's price at that copy's grade, so the page labels it
with the grade. Timestamps are returned as UTC ISO 8601.

There is no monthly budget, so spend is reported with `cap: null` and nothing
is computed against it.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from gixen_overlay.db import list_comics_for_slab_watch
from gixen_overlay.ledger import LedgerRoute
from gixen_overlay.purchases import monthly_spend
from gixen_overlay.routes import _NO_CACHE_HEADERS
from gixen_overlay.slab_alerts import list_slab_alerts

router = APIRouter(route_class=LedgerRoute)


def _utc(ts: str | None) -> str | None:
    """Normalize a stored timestamp to UTC ISO 8601 (naive means UTC)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _market(copies: list[dict[str, Any]], min_grade: float) -> dict[str, Any] | None:
    priced = [c for c in copies if c.get("market") is not None]
    if not priced:
        return None

    def rank(c: dict[str, Any]) -> tuple:
        g = c.get("grade")
        if g is None:
            return (2, 0.0)
        return (0 if g >= min_grade else 1, abs(g - min_grade))

    # Newest sighting first, then a stable sort by grade proximity.
    priced.sort(key=lambda c: _utc(c.get("last_seen_at")) or "", reverse=True)
    best = min(priced, key=rank)
    return {"value": best["market"], "confidence": best.get("confidence"),
            "grade": best.get("grade")}


def build_hunt_list(conn) -> dict[str, Any]:
    by_comic: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for copy in list_slab_alerts(conn):
        by_comic[copy["comic_id"]].append(copy)

    books = []
    for row in list_comics_for_slab_watch(conn):
        if row["slab_min_grade"] is None or row["slab_ceiling"] is None:
            continue
        if row["slab_watch"] == 0:  # hand exclude always wins
            continue
        copies = by_comic.get(row["id"], [])
        alerts = [_utc(c["last_alerted_at"]) for c in copies if c["last_alerted_at"]]
        books.append({
            "comic_id": row["id"],
            "title": row["title"],
            "issue": row["issue"],
            "year": row["year"],
            "min_grade": row["slab_min_grade"],
            "ceiling": row["slab_ceiling"],
            "scanned": bool(copies),
            "live_copies": sum(1 for c in copies if c["ended_at"] is None),
            "last_alerted_at": max(alerts) if alerts else None,
            "market": _market(copies, row["slab_min_grade"]),
        })
    # Title then issue, then a stable pass putting the newest alert first and
    # never-alerted books last.
    books.sort(key=lambda b: (b["title"] or "", b["issue"] or ""))
    alerted = sorted((b for b in books if b["last_alerted_at"]),
                     key=lambda b: b["last_alerted_at"], reverse=True)
    books = alerted + [b for b in books if not b["last_alerted_at"]]

    month = datetime.now(timezone.utc).strftime("%Y-%m")
    entries = [e for e in monthly_spend(conn, months=1) if e["month"] == month]
    return {
        "count": len(books),
        "books": books,
        "spend": {"month": month, "cap": None, "entries": entries},
    }


@router.get("/api/comics/hunt-list")
async def api_hunt_list(request: Request) -> dict[str, Any]:
    return build_hunt_list(request.app.state.db)


@router.get("/comics/hunt-list")
def hunt_list_page():
    return FileResponse(
        Path(__file__).parent / "static" / "hunt-list.html",
        headers=_NO_CACHE_HEADERS,
    )
