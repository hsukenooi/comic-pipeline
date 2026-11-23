"""BUI-1419: storage for high-value slab alerts and their Act/Pass ratings.

One additive table on the comics server's DB, `slab_alerts`, one row per eBay
listing (`item_id` UNIQUE) that `ebay-slab-alerts` (apps/ebay) has seen for a
hunt-list book. It is both the scan's seen-set and the record BUI-1420's
dashboard reads:

* `first_seen_at` / `last_seen_at` / `ended_at` log each copy's life. A copy
  seen again after `ended_at` was set (a search that missed it once) clears
  `ended_at`.
* `price`, `shipping`, `expected`, `market`, `confidence` hold the latest
  observation. `expected` is the all-in price the scan compared.
* `last_alerted_at`, `alerted_expected`, `alerted_best_offer`, and
  `alerted_last_day` freeze the state at the last alert. The scan re-alerts a
  copy only when its expected price dropped below `alerted_expected`, a Best
  Offer appeared, or an auction entered its last day.
* `dropped_reason`, `dropped_at`, and `dropped_price` (BUI-1449) remember a
  copy the scan dropped after getItem (edition, slab_checks, grade_mismatch,
  shipping). The scan skips a copy whose price still equals `dropped_price`.
  Sending `dropped_reason: null` clears the memory. Older DBs gain the three
  columns through an idempotent ALTER in `create_slab_alert_tables`.
* `rating` (`act` / `pass`) and `rated_at` come from the Telegram buttons.

Upsert semantics: only the keys a client sends change. Nothing here bids,
buys, or sends a message.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

RATINGS = ("act", "pass")

# Client-owned columns, in table order. `item_id` is the key; `first_seen_at`
# is set on insert only (from the client's value, else now).
UPSERT_FIELDS = (
    "comic_id", "title", "grade", "listing_type", "best_offer", "price",
    "shipping", "expected", "market", "confidence", "end_date", "last_seen_at",
    "ended_at", "last_alerted_at", "alerted_expected", "alerted_best_offer",
    "alerted_last_day", "dropped_reason", "dropped_at", "dropped_price",
)
_BOOL_FIELDS = ("best_offer", "alerted_best_offer", "alerted_last_day")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_slab_alert_tables(conn: sqlite3.Connection) -> None:
    """Idempotent: CREATE ... IF NOT EXISTS only, safe on an existing DB."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS slab_alerts (
            id                 INTEGER PRIMARY KEY,
            item_id            TEXT NOT NULL UNIQUE,
            comic_id           INTEGER NOT NULL,
            title              TEXT,
            grade              REAL,
            listing_type       TEXT,
            best_offer         INTEGER NOT NULL DEFAULT 0,
            price              REAL,
            shipping           REAL,
            expected           REAL,
            market             REAL,
            confidence         TEXT,
            end_date           TEXT,
            first_seen_at      TEXT NOT NULL,
            last_seen_at       TEXT,
            ended_at           TEXT,
            last_alerted_at    TEXT,
            alerted_expected   REAL,
            alerted_best_offer INTEGER,
            alerted_last_day   INTEGER,
            alert_count        INTEGER NOT NULL DEFAULT 0,
            rating             TEXT CHECK(rating IS NULL OR rating IN ('act', 'pass')),
            rated_at           TEXT,
            dropped_reason     TEXT,
            dropped_at         TEXT,
            dropped_price      REAL,
            created_at         TEXT NOT NULL,
            updated_at         TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_slab_alerts_comic ON slab_alerts(comic_id)"
    )
    # BUI-1449: additive columns for a table created before they existed.
    have = {r[1] for r in conn.execute("PRAGMA table_info(slab_alerts)")}
    for col, typ in (("dropped_reason", "TEXT"), ("dropped_at", "TEXT"),
                     ("dropped_price", "REAL")):
        if col not in have:
            conn.execute(f"ALTER TABLE slab_alerts ADD COLUMN {col} {typ}")


def upsert_slab_alerts(
    conn: sqlite3.Connection, rows: list[dict[str, Any]]
) -> dict[str, int]:
    """Insert or update each row by `item_id`. Commit-free.

    Each row dict carries only the keys the client sent. A row that sets
    `last_alerted_at` bumps `alert_count`. `comic_id` is required on insert.
    """
    inserted = updated = 0
    now = _now()
    for r in rows:
        item_id = r["item_id"]
        values = {k: r[k] for k in UPSERT_FIELDS if k in r}
        for k in _BOOL_FIELDS:
            if values.get(k) is not None:
                values[k] = 1 if values[k] else 0
        bump = 1 if values.get("last_alerted_at") else 0
        exists = conn.execute(
            "SELECT 1 FROM slab_alerts WHERE item_id = ?", (item_id,)
        ).fetchone()
        if exists is None:
            if values.get("comic_id") is None:
                raise ValueError(f"comic_id is required for new item {item_id}")
            if values.get("best_offer") is None:
                values["best_offer"] = 0
            cols = list(values)
            conn.execute(
                f"INSERT INTO slab_alerts (item_id, {', '.join(cols)}, first_seen_at, "
                f"alert_count, created_at, updated_at) "
                f"VALUES (?, {', '.join('?' for _ in cols)}, ?, ?, ?, ?)",
                (item_id, *[values[c] for c in cols],
                 r.get("first_seen_at") or values.get("last_seen_at") or now,
                 bump, now, now),
            )
            inserted += 1
            continue
        if values.get("best_offer") is None:
            values.pop("best_offer", None)  # NOT NULL column: absent means unchanged
        sets = "".join(f"{c} = ?, " for c in values)
        conn.execute(
            f"UPDATE slab_alerts SET {sets}alert_count = alert_count + ?, "
            f"updated_at = ? WHERE item_id = ?",
            (*values.values(), bump, now, item_id),
        )
        updated += 1
    return {"inserted": inserted, "updated": updated}


def record_ratings(
    conn: sqlite3.Connection, ratings: list[dict[str, Any]]
) -> dict[str, Any]:
    """Store Act/Pass ratings. Idempotent: the latest rating wins, and a
    replayed one (a lost getUpdates offset) writes the same value again.
    Ratings for an unknown item id are counted and returned, never stored."""
    recorded, unknown = 0, []
    for r in ratings:
        cur = conn.execute(
            "UPDATE slab_alerts SET rating = ?, rated_at = ?, updated_at = ? "
            "WHERE item_id = ?",
            (r["rating"], r.get("rated_at") or _now(), _now(), r["item_id"]),
        )
        if cur.rowcount:
            recorded += 1
        else:
            unknown.append(r["item_id"])
    return {"recorded": recorded, "unknown": unknown}


def list_slab_alerts(
    conn: sqlite3.Connection,
    *,
    comic_id: int | None = None,
    open_only: bool = False,
    alerted_only: bool = False,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM slab_alerts"
    where: list[str] = []
    params: list[Any] = []
    if comic_id is not None:
        where.append("comic_id = ?")
        params.append(comic_id)
    if open_only:
        where.append("ended_at IS NULL")
    if alerted_only:
        where.append("last_alerted_at IS NOT NULL")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY first_seen_at DESC, id DESC"
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return [dict(r) for r in cur.execute(sql, params).fetchall()]
